---
title: "Categories"
permalink: /categories/
layout: archive
author_profile: false
---

{% assign categories_max = 0 %}
{% for category in site.categories %}
  {% if category[1].size > categories_max %}
    {% assign categories_max = category[1].size %}
  {% endif %}
{% endfor %}

<ul class="taxonomy__index">
  {% for category in site.categories %}
    <li><a href="#{{ category[0] | slugify }}"><strong>{{ category[0] }}</strong> <span class="taxonomy__count">{{ category[1].size }}</span></a></li>
  {% endfor %}
</ul>

{% for category in site.categories %}
  <section id="{{ category[0] | slugify }}" class="taxonomy__section">
    <h2 class="archive__subtitle">{{ category[0] }}</h2>
    {% for post in category[1] %}
      {% include archive-single.html %}
    {% endfor %}
  </section>
{% endfor %}

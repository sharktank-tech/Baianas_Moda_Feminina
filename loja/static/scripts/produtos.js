//
// SLIDER
//

document
.querySelectorAll(".auto-slider")
.forEach(slider=>{

    let index=0;

    const total=
        slider.children.length;

    setInterval(()=>{

        index++;

        if(index>=total){

            index=0;

        }

        slider.style.transform=
            `translateX(-${index*100}%)`;

    },3000);

});


//
// BUSCA
//

function setupSearch(inputId){

    const input=
        document.getElementById(
            inputId
        );

    if(!input) return;

    const cards=
        document.querySelectorAll(
            ".product-card"
        );

    const counter=
        document.getElementById(
            "resultsCounter"
        );

    input.addEventListener(
        "input",
        ()=>{

            let total=0;

            const value=
                input.value.toLowerCase();

            cards.forEach(card=>{

                const text=
                    card.innerText
                    .toLowerCase();

                if(
                    text.includes(value)
                ){

                    card.style.display=
                        "block";

                    total++;

                }

                else{

                    card.style.display=
                        "none";

                }

            });

            counter.innerHTML=
                `Exibindo ${total} produtos`;

        }
    );

}

setupSearch(
    "searchProducts"
);

setupSearch(
    "searchProductsMobile"
);


//
// FAVORITOS
//

let favorites=
    JSON.parse(
        localStorage.getItem(
            "favorites"
        ) || "[]"
    );

document
.querySelectorAll(
    ".favorite-btn"
)
.forEach(btn=>{

    const id=
        btn.dataset.id;

    if(
        favorites.includes(id)
    ){

        btn.innerHTML="❤️";

    }

    btn.addEventListener(
        "click",
        e=>{

            e.preventDefault();

            if(
                favorites.includes(id)
            ){

                favorites=
                    favorites.filter(
                        x=>x!==id
                    );

                btn.innerHTML="♡";

            }

            else{

                favorites.push(id);

                btn.innerHTML="❤️";

            }

            localStorage.setItem(
                "favorites",
                JSON.stringify(
                    favorites
                )
            );

        }
    );

});


//
// DRAWER MOBILE
//

const openBtn=
    document.getElementById(
        "openFilters"
    );

const closeBtn=
    document.getElementById(
        "closeFilters"
    );

const drawer=
    document.getElementById(
        "mobileFilters"
    );

openBtn?.addEventListener(
    "click",
    ()=>{

        drawer.classList.remove(
            "hidden"
        );

    }
);

closeBtn?.addEventListener(
    "click",
    ()=>{

        drawer.classList.add(
            "hidden"
        );

    }
);